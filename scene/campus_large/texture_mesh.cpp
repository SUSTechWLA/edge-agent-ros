/* Orthographic XY rasterization for the locked Fuel OBJ and Collada meshes.
 * Python owns the SDF transforms, raster bounds and output files. The native
 * pass uses Assimp/PNG already shipped in the Gazebo build image. */
#include <assimp/Importer.hpp>
#include <assimp/config.h>
#include <assimp/postprocess.h>
#include <assimp/scene.h>
#include <png.h>
#include <algorithm>
#include <cmath>
#include <filesystem>
#include <iostream>
#include <string>
#include <vector>

namespace fs = std::filesystem;
struct Pixel { unsigned char r,g,b,a; };
struct Vertex { double x,y,z,u,v; };

struct Image {
    png_image info{};
    std::vector<Pixel> pixels;
    bool load(const fs::path& path) {
        info.version = PNG_IMAGE_VERSION;
        if (!png_image_begin_read_from_file(&info,path.c_str())) return false;
        info.format = PNG_FORMAT_RGBA;
        pixels.resize(info.width * size_t(info.height));
        bool ok = png_image_finish_read(&info,nullptr,pixels.data(),0,nullptr) != 0;
        png_image_free(&info);
        return ok;
    }
    Pixel at(double u,double v) const {
        if (pixels.empty()) return {255,255,255,255};
        // OBJ/Collada have bottom-left UV; PNG stores the top row first.
        unsigned x = std::min(info.width-1, unsigned((u-std::floor(u))*info.width));
        unsigned y = std::min(info.height-1, unsigned((1.-(v-std::floor(v)))*info.height));
        return pixels[y*size_t(info.width)+x];
    }
};

struct Raster {
    int w,h;
    double xmin,ymax,px,py;
    unsigned char* color;
    float* heights;
    void triangle(Vertex a,Vertex b,Vertex c,const Image& image,const aiColor3D& kd) {
        double dx=b.x-a.x,dy=b.y-a.y,ex=c.x-a.x,ey=c.y-a.y;
        const double area=dx*ey-dy*ex;
        if (std::abs(area)<1e-8) return;
        const double dz=b.z-a.z,ez=c.z-a.z;
        double nx=dy*ez-dz*ey, ny=dz*ex-dx*ez, nz=area;
        double length=std::sqrt(nx*nx+ny*ny+nz*nz);
        double sun=(.42*nx-.32*ny+.80*nz)*(nz>0?1.:-1.)/length;
        double shade=.74+.26*std::clamp(sun/.80,0.,1.25);
        int x0=std::max(0,int(std::floor((std::min({a.x,b.x,c.x})-xmin)/px)));
        int x1=std::min(w-1,int(std::floor((std::max({a.x,b.x,c.x})-xmin)/px)));
        int y0=std::max(0,int(std::floor((ymax-std::max({a.y,b.y,c.y}))/py)));
        int y1=std::min(h-1,int(std::floor((ymax-std::min({a.y,b.y,c.y}))/py)));
        for (int row=y0;row<=y1;row++) for (int col=x0;col<=x1;col++) {
            double x=xmin+(col+.5)*px-a.x, y=ymax-(row+.5)*py-a.y;
            double s=(x*ey-y*ex)/area, t=(dx*y-dy*x)/area;
            if (s<0 || t<0 || s+t>1) continue;
            double z=a.z+s*dz+t*ez;
            size_t i=row*size_t(w)+col;
            if (z<heights[i]) continue;
            Pixel tex=image.at(a.u+s*(b.u-a.u)+t*(c.u-a.u),a.v+s*(b.v-a.v)+t*(c.v-a.v));
            if (tex.a<100) continue;
            heights[i]=z;
            color[i*3]=std::clamp(tex.r*kd.r*shade,0.,255.);
            color[i*3+1]=std::clamp(tex.g*kd.g*shade,0.,255.);
            color[i*3+2]=std::clamp(tex.b*kd.b*shade,0.,255.);
        }
    }
};

// The pinned OBJ lives in Z-up world coordinates. The pine DAE specifies
// Z_UP and carries its real 0.0254 node scale; keep both before placement.
extern "C" int mesh(const char* path,const char* textureDir,int pine,const double* placement,
                    int w,int h,const double* extent,unsigned char* color,float* heights) {
    try {
        Assimp::Importer importer;
        importer.SetPropertyBool(AI_CONFIG_IMPORT_COLLADA_IGNORE_UP_DIRECTION,true);
        const aiScene* scene=importer.ReadFile(path,aiProcess_Triangulate|aiProcess_PreTransformVertices);
        if (!scene) throw std::runtime_error(importer.GetErrorString());
        Raster surface{w,h,extent[0],extent[3],(extent[2]-extent[0])/w,(extent[3]-extent[1])/h,color,heights};
        const double co=std::cos(placement[3]),si=std::sin(placement[3]);
        for (unsigned m=0;m<scene->mNumMeshes;m++) {
            const aiMesh& shape=*scene->mMeshes[m];
            const aiMaterial& material=*scene->mMaterials[shape.mMaterialIndex];
            aiColor3D kd(0.8f);
            material.Get(AI_MATKEY_COLOR_DIFFUSE,kd);
            aiString texture,materialName;
            material.Get(AI_MATKEY_NAME,materialName);
            fs::path png;
            if (pine) {
                bool bark=std::string(materialName.C_Str()).find("Bark")!=std::string::npos;
                png=fs::path(textureDir)/(bark?"bark_diffuse.png":"branch_2_diffuse.png");
                kd=aiColor3D(1.,1.,1.);
            } else if (material.GetTexture(aiTextureType_DIFFUSE,0,&texture)==AI_SUCCESS) {
                png=fs::path(textureDir)/fs::path(texture.C_Str()).filename();
            }
            Image image;
            if (!png.empty() && !image.load(png)) throw std::runtime_error("Could not decode "+png.string());
            auto vertex=[&](unsigned index) {
                const auto& p=shape.mVertices[index];
                aiVector3D uv=shape.HasTextureCoords(0)?shape.mTextureCoords[0][index]:aiVector3D();
                return Vertex{placement[0]+co*p.x-si*p.y,placement[1]+si*p.x+co*p.y,
                              placement[2]+p.z,uv.x,uv.y};
            };
            for (unsigned f=0;f<shape.mNumFaces;f++) {
                const auto& face=shape.mFaces[f];
                if (face.mNumIndices==3) surface.triangle(vertex(face.mIndices[0]),vertex(face.mIndices[1]),
                                                         vertex(face.mIndices[2]),image,kd);
            }
        }
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << std::endl;
        return 1;
    }
}
