#include <filesystem>
#include <iostream>

#include <openskp/instanced_glb.hpp>
#include <openskp/openskp.hpp>

int main(int argc, char** argv) {
    if (argc != 3) {
        std::cerr << "Uso: skp2glb <entrada.skp> <salida.glb>\n";
        return 2;
    }

    const std::filesystem::path input = argv[1];
    const std::filesystem::path output = argv[2];

    try {
        std::cerr << "[native] Abriendo SKP: " << input << "\n";

        auto skp = openskp::SkpFile::open(input);

        std::cerr << "[native] Construyendo escena instanciada...\n";

        const auto scene = skp.build_instanced_scene();

        std::cerr << "[native] Mesh resources: "
                  << scene.mesh_resources.size()
                  << "\n";

        openskp::InstancedGlbOptions options;
        options.textures = true;

        std::cerr << "[native] Exportando GLB con texturas embebidas...\n";

        openskp::export_instanced_glb(
            scene,
            output,
            options
        );

        if (!std::filesystem::exists(output) ||
            std::filesystem::file_size(output) == 0) {

            std::cerr << "[native] No se genero un GLB valido.\n";
            return 3;
        }

        std::cerr << "[native] GLB generado: "
                  << std::filesystem::file_size(output)
                  << " bytes\n";

        return 0;

    } catch (const std::exception& error) {

        std::cerr << "[native] ERROR: "
                  << error.what()
                  << "\n";

        return 1;

    } catch (...) {

        std::cerr << "[native] ERROR desconocido.\n";

        return 1;
    }
}