// Convert indexed Assimp geometry to a small intermediate stream for the GLB writer.
// Input meshes stay in their source Z-up axes including COLLADA node transforms.
#include <assimp/Importer.hpp>
#include <assimp/config.h>
#include <assimp/postprocess.h>
#include <assimp/scene.h>
#include <png.h>
#include <algorithm>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

template<class T> void put(std::ostream& out, T v) {
    out.write(reinterpret_cast<const char*>(&v), sizeof(v));
}
void string(std::ostream& out, const aiString& value) {
    put(out, uint32_t(value.length));
    out.write(value.C_Str(), value.length);
}

void geometry(const char* input, const char* path) {
    Assimp::Importer loader;
    loader.SetPropertyBool(AI_CONFIG_IMPORT_COLLADA_IGNORE_UP_DIRECTION, true);
    const aiScene* scene = loader.ReadFile(input, aiProcess_Triangulate | aiProcess_PreTransformVertices |
        aiProcess_GenNormals | aiProcess_JoinIdenticalVertices | aiProcess_SortByPType);
    if (!scene) throw std::runtime_error(loader.GetErrorString());
    std::ofstream output(path, std::ios::binary);
    for (unsigned i=0; i<scene->mNumMeshes; ++i) {
        const auto& mesh = *scene->mMeshes[i];
        // Triangulated quads also carry aiPrimitiveType_NGONEncodingFlag.
        if (!(mesh.mPrimitiveTypes & aiPrimitiveType_TRIANGLE) || !mesh.mNumFaces) continue;
        const auto& mat = *scene->mMaterials[mesh.mMaterialIndex];
        aiString name, texture;
        mat.Get(AI_MATKEY_NAME, name);
        mat.GetTexture(aiTextureType_DIFFUSE, 0, &texture);
        string(output, mesh.mName); string(output, name); string(output, texture);
        aiColor3D kd(0.8f,0.8f,0.8f);
        mat.Get(AI_MATKEY_COLOR_DIFFUSE, kd);
        put(output, kd.r); put(output,kd.g); put(output,kd.b);
        put(output, uint32_t(mesh.mNumVertices)); put(output, uint32_t(3*mesh.mNumFaces));
        for (unsigned v=0; v<mesh.mNumVertices; ++v) {
            const auto p=mesh.mVertices[v], n=mesh.mNormals[v];
            const auto uv=mesh.HasTextureCoords(0) ? mesh.mTextureCoords[0][v] : aiVector3D();
            for (float f : {p.x,p.y,p.z,n.x,n.y,n.z,uv.x,1.f-uv.y}) put(output,f);
        }
        for (unsigned j=0; j<mesh.mNumFaces; ++j) {
            const auto& face=mesh.mFaces[j];
            if (face.mNumIndices!=3) throw std::runtime_error("Invalid Assimp triangle");
            for (unsigned k=0; k<3; ++k) put(output,uint32_t(face.mIndices[k]));
        }
    }
    if (!output) throw std::runtime_error("Cannot write mesh stream");
}

// Box-filter large original diffuse maps for browser GPUs. Keep leaf alpha.
void thumbnail(const char* input, const char* output, unsigned limit) {
    png_image png{};
    png.version=PNG_IMAGE_VERSION;
    if (!png_image_begin_read_from_file(&png,input)) throw std::runtime_error(png.message);
    png.format=PNG_FORMAT_RGBA;
    std::vector<unsigned char> pixels(PNG_IMAGE_SIZE(png));
    if (!png_image_finish_read(&png,nullptr,pixels.data(),0,nullptr)) throw std::runtime_error(png.message);
    unsigned w=png.width, h=png.height;
    while (std::max(w,h)>limit) {
        unsigned nw=std::max(1u,(w+1)/2), nh=std::max(1u,(h+1)/2);
        std::vector<unsigned char> small(nw*size_t(nh)*4);
        for (unsigned y=0; y<nh; ++y) for (unsigned x=0; x<nw; ++x) {
            unsigned count=0, rgba[4]={};
            for (unsigned j=2*y; j<std::min(h,2*y+2); ++j)
                for (unsigned i=2*x; i<std::min(w,2*x+2); ++i) {
                    for (int k=0; k<4; ++k) rgba[k]+=pixels[4*(size_t(j)*w+i)+k];
                    ++count;
                }
            for (int k=0;k<4;++k) small[4*(size_t(y)*nw+x)+k]=(rgba[k]+count/2)/count;
        }
        pixels.swap(small); w=nw; h=nh;
    }
    png.width=w; png.height=h;
    if (!png_image_write_to_file(&png,output,0,pixels.data(),0,nullptr)) throw std::runtime_error(png.message);
    png_image_free(&png);
}
int main(int argc, char** argv) {
    try {
        if (argc==4 && std::string(argv[1])=="geometry") geometry(argv[2],argv[3]);
        else if (argc==5 && std::string(argv[1])=="image") thumbnail(argv[2],argv[3],std::stoul(argv[4]));
        else throw std::runtime_error("Expected geometry INPUT OUTPUT or image INPUT OUTPUT LIMIT");
    } catch (const std::exception& err) { std::cerr<<err.what()<<'\n'; return 1; }
}
